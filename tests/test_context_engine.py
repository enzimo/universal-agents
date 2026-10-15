from __future__ import annotations

import json

import pytest

from assistant_gateway.config import GatewayInstanceConfig
from custom_agents.conversation_delivery_states import (
    DELIVERY_STATE_NOTIFICATION_QUEUED,
)
from custom_agents.context_engine import (
    ContextAssemblyRequest,
    ContextBudgetExceededError,
    ContextEngineAdapters,
    DefaultContextEngine,
    TurnFinalizationRequest,
)
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from runtime_settings.instance_layout import InstanceLayout


def _request(**kwargs) -> ContextAssemblyRequest:
    """Ordinary test turns use the same metadata for identity and rendering."""

    execution_task_id = kwargs.pop("execution_task_id", getattr(kwargs["incoming_content"], "task_id", None))
    return ContextAssemblyRequest(identity_meta=kwargs["incoming_meta"], execution_task_id=execution_task_id, **kwargs)


class _TaskManager:
    def __init__(self, runtime_timing: str = "") -> None:
        from custom_agents.task_operation_runtime import TaskOperationRuntime

        self.operations_runtime = TaskOperationRuntime()
        self.runtime_timing = runtime_timing

    def render_execution_digest_markdown(self, *, task_id: str) -> str:
        assert task_id == "task_context_1"
        return "## Execution Digest\n- **Goal:** Keep context bounded."

    def summarize_task_history_markdown(
        self, *, task_id: str, include_execution_digest: bool = True
    ) -> str:
        assert task_id == "task_context_1"
        assert include_execution_digest is False
        return (
            "# Task Execution Summary: `task_context_1`\n\n## Rolling History\n- queued"
        )

    def render_task_runtime_timing_markdown(self, task_id: str, **kwargs) -> str:
        assert task_id == "task_context_1"
        return self.runtime_timing

    def render_task_query_history_markdown(self, task_id: str, *, latest_only=False) -> str:
        return ""


class _ConversationCompactor:
    def compact_if_needed(self, **kwargs):
        raise AssertionError(
            "compact_if_needed should not be called during phase-one assembly"
        )


class _ConversationStore:
    def __init__(self, turns=None):
        self.turns = turns or [
            {
                "conversation_id": "conversation_context_1",
                "task_id": "prior_task_1",
                "turn_index": 1,
                "task_status": "completed",
                "problem_text": "Earlier ask",
                "final_answer_text": "Earlier answer",
                "turn_summary_text": "Earlier ask was answered.",
                "tool_artifact_manifest": [],
                "requester_type": "user_req",
                "delivery_state": "not_applicable",
            }
        ]

    def get_recent_turns(
        self, conversation_id, limit=None, *, include_pending_delivery=True
    ):
        assert conversation_id == "conversation_context_1"
        assert include_pending_delivery is False
        return list(self.turns[:limit])


class _CountingModel:
    def __init__(self) -> None:
        self.calls = []

    async def count_tokens(
        self, messages, tool_specs=None, system_prompt=None, system_prompt_content=None
    ):
        self.calls.append(
            {
                "messages": messages,
                "tool_specs": tool_specs,
                "system_prompt": system_prompt,
                "system_prompt_content": system_prompt_content,
            }
        )
        rendered = json.dumps(messages, ensure_ascii=False, default=str)
        return max(1, len(rendered) // 4)


class _MemoryPluginManager:
    def hydrate(self, request):
        from custom_agents.context_engine.base import ContextSection

        return [
            ContextSection(
                name="coremem",
                text="Core Memory:\n[1] Team prefers explicit smoke tests.",
                priority=34,
                pinned=True,
                source="PinnedMemoryRepository.list_coremem",
                char_count=len("Core Memory:\n[1] Team prefers explicit smoke tests."),
                estimated_tokens=12,
                plugin_id="coremem",
                scope_kind="team_id",
                degradable=False,
            )
        ]


def _engine(
    *, context_window_tokens: int = 10000, task_manager=None, **kwargs
) -> DefaultContextEngine:
    kwargs.setdefault("model", _CountingModel())
    return DefaultContextEngine(
        agent_id="agent_1",
        logger=None,
        task_manager=task_manager or _TaskManager(),
        conversation_store=_ConversationStore(),
        conversation_compactor=_ConversationCompactor(),
        system_prompt="System prompt text",
        context_window_tokens=context_window_tokens,
        reserved_output_tokens=kwargs.pop("reserved_output_tokens", 0),
        **kwargs,
        adapters=ContextEngineAdapters(
            build_request_time_context=lambda meta: "Request Time Context:\n- now",
            build_request_conversation_context=lambda meta, request_text: (
                "Request Conversation Context:\n- thread"
            ),
            build_team_routing_prompt_context=lambda: "route to specialists",
            verified_person_of=lambda meta: None,
        ),
    )


def test_default_context_engine_uses_model_count_tokens_when_available():
    model = _CountingModel()
    task = Task(
        task_id="task_context_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Use the model token counter.",
    )

    assembly = _engine(model=model).assemble(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(),
            incoming_meta=MessageMeta(
                sender="user",
                receiver="assistant",
                requester_type="user_req",
                conversation_id="conversation_context_1",
            ),
            incoming_message_json=json.dumps(
                {
                    "message_class": "CommsMessage",
                    "msg_content": {"task_id": task.task_id},
                }
            ),
        )
    )

    assert assembly.estimated_tokens > 0
    assert model.calls
    assert any(call["messages"][0]["content"][0].get("text") for call in model.calls)


def test_default_context_engine_accounts_for_provider_prompt_suffix():
    task = Task(
        task_id="task_context_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Use the final provider prompt.",
    )
    suffix = "Terminal structured-output instruction: call the decision tool."

    assembly = _engine().assemble(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(from_agent_id="user", agent_signature=""),
            incoming_meta=MessageMeta(
                from_agent_name="user",
                conversation_id="conversation_context_1",
            ),
            incoming_message_json="{}",
            provider_prompt_suffix=suffix,
        )
    )

    assert assembly.prompt.endswith(suffix)
    assert assembly.char_count == len(assembly.prompt) + len("System prompt text")
    suffix_section = next(
        section
        for section in assembly.sections
        if section.name == "provider_prompt_suffix"
    )
    assert suffix_section.text == suffix
    assert suffix_section.pinned is True
    assert any(
        item["name"] == "provider_prompt_suffix" for item in assembly.source_lengths
    )


def test_default_context_engine_assembles_prompt_sections_with_budgeted_thread_accounting():
    task = Task(
        task_id="task_context_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Use the prior turn.",
    )
    message_json = json.dumps(
        {"message_class": "CommsMessage", "msg_content": {"task_id": task.task_id}}
    )
    assembly = _engine(context_window_tokens=10000).assemble(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(from_agent_id="user", agent_signature=""),
            incoming_meta=MessageMeta(
                from_agent_name="user",
                conversation_id="conversation_context_1",
                msg_correlation_id="corr_context_1",
            ),
            incoming_message_json=message_json,
        )
    )

    prompt = assembly.prompt
    assert prompt.index("Request Time Context:") < prompt.index(
        "Request Conversation Context:"
    )
    assert prompt.index("Request Conversation Context:") < prompt.index(
        "Team Routing Context:"
    )
    assert prompt.index("Team Routing Context:") < prompt.index(
        "Conversation Thread Context:"
    )
    assert prompt.index("Conversation Thread Context:") < prompt.index(
        "## Execution Digest"
    )
    assert prompt.index("## Execution Digest") < prompt.index("Task History:")
    assert prompt.index("Incoming Message Envelope:") < prompt.index("Current Request:")
    assert prompt.count("## Execution Digest") == 1
    assert "### Turn 1 [full] task_id=prior_task_1" in prompt
    assert "Earlier ask" in prompt
    assert "Earlier answer" in prompt
    assert "Earlier ask was answered." not in prompt
    assert "Turn summary:" not in prompt
    assert "Incoming Message Envelope:" in prompt
    assert not prompt.endswith(message_json)
    assert prompt.rstrip().endswith("Current Request:\nUse the prior turn.")

    sources = {item["name"]: item for item in assembly.source_lengths}
    assert sources["system_prompt"]["chars"] == len("System prompt text")
    assert (
        sources["current_request"]["source"]
        == "prompt_context_sanitization.render_current_request_context"
    )
    assert (
        sources["incoming_message"]["source"]
        == "prompt_context_sanitization.render_incoming_message_summary"
    )
    assert sources["incoming_message"]["chars"] == len(
        next(
            section.text
            for section in assembly.sections
            if section.name == "incoming_message"
        )
    )
    assert sources["conversation_thread_context"]["pinned"] is False
    assert (
        sources["conversation_thread_context"]["source"]
        == "BudgetedThreadContextRenderer.render"
    )
    assert sources["execution_digest"]["pinned"] is True
    assert assembly.max_context_tokens == 10000
    assert assembly.soft_limit_tokens == 8500
    assert assembly.estimated_tokens > 0
    assert assembly.context_fill_ratio is not None


def test_default_context_engine_adds_memory_plugin_sections_before_thread_context():
    task = Task(
        task_id="task_context_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Use plugin memory.",
    )
    assembly = _engine(memory_plugin_manager=_MemoryPluginManager()).assemble(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(from_agent_id="user", agent_signature=""),
            incoming_meta=MessageMeta(
                from_agent_name="user",
                conversation_id="conversation_context_1",
                msg_correlation_id="corr_context_1",
            ),
            incoming_message_json=json.dumps(
                {"msg_content": {"task_id": task.task_id}}
            ),
        )
    )

    assert assembly.prompt.index("Team Routing Context:") < assembly.prompt.index(
        "Core Memory:"
    )
    assert assembly.prompt.index("Core Memory:") < assembly.prompt.index(
        "Conversation Thread Context:"
    )
    sources = {item["name"]: item for item in assembly.source_lengths}
    assert sources["coremem"]["plugin_id"] == "coremem"
    assert sources["coremem"]["scope_kind"] == "team_id"
    assert sources["coremem"]["source"] == "PinnedMemoryRepository.list_coremem"


def test_default_context_engine_has_no_profile_memory_fallback_when_plugin_manager_missing():
    task = Task(
        task_id="task_context_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Use compact memory.",
    )
    assembly = _engine(context_window_tokens=10000).assemble(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(from_agent_id="user", agent_signature=""),
            incoming_meta=MessageMeta(
                from_agent_name="user",
                conversation_id="conversation_context_1",
                msg_correlation_id="corr_context_1",
            ),
            incoming_message_json=json.dumps(
                {"msg_content": {"task_id": task.task_id}}
            ),
            degraded_sections=("domain_topic_recall",),
        )
    )

    assert "User Preferences Memory:" not in assembly.prompt
    assert "Team Memory:" not in assembly.prompt
    assert "Long-Term Memory:" not in assembly.prompt
    sources = {item["name"]: item for item in assembly.source_lengths}
    assert "user_preference_memory" not in sources
    assert "team_memory" not in sources


def test_default_context_engine_includes_task_runtime_timing_when_enabled():
    task = Task(
        task_id="task_context_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Use timing.",
    )
    timing = (
        "Task Runtime Timing:\n"
        "- **Message created:** 2026-05-05T10:00:00+00:00\n"
        "- **Accepted by this agent:** 2026-05-05T10:00:02+00:00\n"
        "- **Processing started:** 2026-05-05T10:00:03+00:00"
    )
    assembly = _engine(task_manager=_TaskManager(runtime_timing=timing)).assemble(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(from_agent_id="user", agent_signature=""),
            incoming_meta=MessageMeta(
                from_agent_name="user",
                conversation_id="conversation_context_1",
                msg_correlation_id="corr_context_1",
            ),
            incoming_message_json=json.dumps(
                {"msg_content": {"task_id": task.task_id}}
            ),
        )
    )

    assert "Task Runtime Timing:" in assembly.prompt
    assert assembly.prompt.index("Task History:") < assembly.prompt.index(
        "Task Runtime Timing:"
    )
    assert assembly.prompt.index("Task Runtime Timing:") < assembly.prompt.index(
        "Incoming Message Envelope:"
    )
    sources = {item["name"]: item for item in assembly.source_lengths}
    assert (
        sources["task_runtime_timing"]["source"]
        == "TaskManager.render_task_runtime_timing_markdown"
    )


def test_default_context_engine_omits_task_runtime_timing_when_disabled():
    task = Task(
        task_id="task_context_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Use timing.",
    )
    assembly = _engine(
        task_manager=_TaskManager(runtime_timing="Task Runtime Timing:\n- hidden"),
        task_runtime_timing={"enabled": False},
    ).assemble(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(from_agent_id="user", agent_signature=""),
            incoming_meta=MessageMeta(
                from_agent_name="user",
                conversation_id="conversation_context_1",
                msg_correlation_id="corr_context_1",
            ),
            incoming_message_json=json.dumps(
                {"msg_content": {"task_id": task.task_id}}
            ),
        )
    )

    assert "Task Runtime Timing:" not in assembly.prompt
    assert "task_runtime_timing" not in {
        item["name"] for item in assembly.source_lengths
    }


def test_default_context_engine_preserves_sanitized_inner_request_for_recovery_prompt():
    task = Task(
        task_id="task_context_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Recover compactly.",
    )
    message_json = json.dumps(
        {"msg_meta": {"conversation_id": "current_recovery_conversation"}}
    )
    assembly = _engine().assemble(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(from_agent_id="user", agent_signature=""),
            incoming_meta=MessageMeta(
                from_agent_name="user",
                conversation_id="conversation_context_1",
                msg_correlation_id="corr_context_1",
            ),
            incoming_message_json=message_json,
            isolated_recovery_task=True,
        )
    )

    assert "Request Time Context:" in assembly.prompt
    assert "Team Routing Context:" in assembly.prompt
    assert "Current Request:" in assembly.prompt
    assert "Request Conversation Context:" not in assembly.prompt
    assert "User Preferences Memory:" not in assembly.prompt
    assert "Team Memory:" not in assembly.prompt
    assert "Long-Term Memory:" not in assembly.prompt
    assert "Conversation Thread Context:" not in assembly.prompt
    assert "current_recovery_conversation" not in assembly.prompt
    assert "Incoming Message Envelope:" in assembly.prompt


def test_default_context_engine_omits_old_thread_turns_when_thread_budget_is_exhausted():
    turns = []
    for index in range(1, 9):
        turns.append(
            {
                "conversation_id": "conversation_context_1",
                "task_id": f"prior_task_{index}",
                "turn_index": index,
                "task_status": "completed",
                "problem_text": f"Earlier request {index} " + ("x" * 500),
                "final_answer_text": f"Earlier answer {index} " + ("y" * 500),
                "turn_summary_text": f"Summary for turn {index}.",
                "tool_artifact_manifest": [],
                "requester_type": "user_req",
                "delivery_state": "not_applicable",
            }
        )
    engine = _engine(context_window_tokens=1200)
    engine.conversation_store = _ConversationStore(turns)
    task = Task(
        task_id="task_context_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Use the prior turn.",
    )
    message_json = json.dumps(
        {"message_class": "CommsMessage", "msg_content": {"task_id": task.task_id}}
    )

    assembly = engine.assemble(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(from_agent_id="user", agent_signature=""),
            incoming_meta=MessageMeta(
                from_agent_name="user",
                conversation_id="conversation_context_1",
                msg_correlation_id="corr_context_1",
            ),
            incoming_message_json=message_json,
        )
    )

    assert "Conversation Thread Context:" in assembly.prompt
    assert "Context trimmed:" in assembly.prompt
    assert (
        'get_conversation_context_slice(conversation_id="conversation_context_1"'
        in assembly.prompt
    )
    assert "prior_task_8" in assembly.prompt
    assert "prior_task_1" not in assembly.prompt


def test_default_context_engine_includes_notification_queued_timer_turns():
    engine = _engine()
    engine.conversation_store = _ConversationStore(
        [
            {
                "conversation_id": "conversation_context_1",
                "task_id": "timer_pending",
                "turn_index": 1,
                "task_status": "completed",
                "problem_text": "Check the match.",
                "final_answer_text": "Pending timer answer should not be visible.",
                "turn_summary_text": "Pending timer summary should not be visible.",
                "tool_artifact_manifest": [],
                "requester_type": "timer_req",
                "delivery_state": "pending_gateway",
            },
            {
                "conversation_id": "conversation_context_1",
                "task_id": "timer_notified",
                "turn_index": 2,
                "task_status": "completed",
                "problem_text": "Check the match.",
                "final_answer_text": "Goal scored by Example FC at 12'.",
                "turn_summary_text": "Reported a goal scored by Example FC at 12'.",
                "tool_artifact_manifest": [],
                "requester_type": "timer_req",
                "delivery_state": DELIVERY_STATE_NOTIFICATION_QUEUED,
            },
        ]
    )
    task = Task(
        task_id="task_context_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Check the match again.",
    )
    assembly = engine.assemble(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(from_agent_id="timer", agent_signature=""),
            incoming_meta=MessageMeta(
                from_agent_name="timer",
                conversation_id="conversation_context_1",
                msg_correlation_id="corr_context_1",
            ),
            incoming_message_json=json.dumps({"message_class": "CommsMessage"}),
        )
    )

    assert "Goal scored by Example FC" in assembly.prompt
    assert "Pending timer answer should not be visible" not in assembly.prompt


def test_default_context_engine_adds_model_visible_soft_budget_warning():
    task = Task(
        task_id="task_context_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Use the prior turn.",
    )
    message_json = json.dumps({"message_class": "CommsMessage", "payload": "x" * 200})

    assembly = _engine(
        context_window_tokens=600,
        soft_context_ratio=0.2,
        reserved_output_tokens=100,
    ).assemble(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(from_agent_id="user", agent_signature=""),
            incoming_meta=MessageMeta(
                from_agent_name="user",
                conversation_id="conversation_context_1",
                msg_correlation_id="corr_context_1",
            ),
            incoming_message_json=message_json,
        )
    )

    assert "Context Budget Warning:" in assembly.prompt
    assert any(
        warning["warning"] == "context_soft_limit_reached"
        for warning in assembly.warnings
    )
    assert assembly.hard_limit_tokens == 500
    assert assembly.reserved_output_tokens == 100


def test_default_context_engine_raises_when_hard_budget_is_exceeded():
    task = Task(
        task_id="task_context_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Use the prior turn. " + ("x" * 5000),
    )
    message_json = json.dumps(
        {"message_class": "CommsMessage", "payload": "raw json should be compacted"}
    )

    with pytest.raises(ContextBudgetExceededError) as exc_info:
        _engine(context_window_tokens=300, reserved_output_tokens=100).assemble(
            _request(
                incoming_content=task,
                incoming_header=MessageHeader(from_agent_id="user", agent_signature=""),
                incoming_meta=MessageMeta(
                    from_agent_name="user",
                    conversation_id="conversation_context_1",
                    msg_correlation_id="corr_context_1",
                ),
                incoming_message_json=message_json,
            )
        )

    assert exc_info.value.hard_limit_tokens == 200
    assert exc_info.value.source_lengths


def test_default_context_engine_compacts_gateway_request_payload():
    task = Task(
        task_id="task_context_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement=(
            "[Gateway Context]\n"
            "- Origin: interactive\n"
            "- Session Key: session_test\n\n"
            "[User Profile]\n"
            "Name: Taylor\n\n"
            "[Delivery Preferences]\n"
            "Default delivery channel: telegram\n\n"
            "[Request]\n"
            "hello"
        ),
    )
    message_json = json.dumps(
        {
            "message_class": "CommsMessage",
            "msg_content": {
                "task_id": task.task_id,
                "problem_statement": task.problem_statement,
                "execution_history": [{"summary": task.problem_statement}],
            },
        }
    )
    assembly = _engine().assemble(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(
                from_agent_id="user",
                agent_signature="",
                response_routing_key="user_queue",
            ),
            incoming_meta=MessageMeta(
                from_agent_name="Taylor",
                conversation_id="conversation_context_1",
                msg_correlation_id="corr_context_1",
                request_timezone="America/Chicago",
                gateway_context=GatewayContext(
                    gateway_instance_name="nova",
                    session_key="session_test",
                    delivery_mode="interactive",
                    channel_name="telegram",
                ),
            ),
            incoming_message_json=message_json,
        )
    )

    assert "Current Request:\nhello" in assembly.prompt
    assert "Current User Context:" in assembly.prompt
    assert "Name: Taylor" in assembly.prompt
    assert "Default delivery channel: telegram" in assembly.prompt
    assert "--- Incoming Message ---" not in assembly.prompt
    assert '"problem_statement"' not in assembly.prompt
    assert assembly.prompt.count("[Request]") == 0
    assert assembly.prompt.index("Gateway Delivery Context:") < assembly.prompt.index(
        "Current Request:"
    )
    assert assembly.prompt.rstrip().endswith("Current Request:\nhello")


def _soul_layout(tmp_path) -> InstanceLayout:
    """An instance whose TeamLead SOUL.md sits in control/."""
    layout = InstanceLayout.at(tmp_path)
    layout.ensure_directories()
    layout.soul_path.write_text(
        "# Assistant Identity\n\nName: Nova\n", encoding="utf-8"
    )
    return layout


def test_default_context_engine_hydrates_team_lead_soul_each_turn(tmp_path):
    layout = _soul_layout(tmp_path)
    task = Task(
        task_id="task_context_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="hello",
    )
    message_json = json.dumps(
        {"message_class": "CommsMessage", "msg_content": {"task_id": task.task_id}}
    )
    engine = _engine(
        workspace_dir=str(layout.workspaces_dir),
        instance_root=str(layout.root),
        is_team_lead=True,
    )

    first = engine.assemble(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(from_agent_id="user", agent_signature=""),
            incoming_meta=MessageMeta(
                from_agent_name="Taylor",
                conversation_id="conversation_context_1",
                msg_correlation_id="corr_context_1",
            ),
            incoming_message_json=message_json,
        )
    )
    assert "Workspace Turn Context:" in first.prompt
    assert "Name: Nova" in first.prompt

    layout.soul_path.write_text(
        "# Assistant Identity\n\nName: Nova\nStyle: direct\n", encoding="utf-8"
    )
    second = engine.assemble(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(from_agent_id="user", agent_signature=""),
            incoming_meta=MessageMeta(
                from_agent_name="Taylor",
                conversation_id="conversation_context_1",
                msg_correlation_id="corr_context_1",
            ),
            incoming_message_json=message_json,
        )
    )

    assert "Style: direct" in second.prompt
    sources = {item["name"]: item for item in second.source_lengths}
    assert (
        sources["workspace_user_context"]["source"]
        == "workspace_prompt_context.build_workspace_turn_context"
    )


def test_default_context_engine_does_not_hydrate_soul_for_specialist(tmp_path):
    layout = _soul_layout(tmp_path)
    task = Task(
        task_id="task_context_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="hello",
    )
    engine = _engine(
        workspace_dir=str(layout.workspaces_dir),
        instance_root=str(layout.root),
        is_team_lead=False,
    )

    assembly = engine.assemble(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(from_agent_id="manager", agent_signature=""),
            incoming_meta=MessageMeta(
                from_agent_name="Manager",
                conversation_id="conversation_context_1",
                msg_correlation_id="corr_context_specialist",
            ),
            incoming_message_json=json.dumps(
                {
                    "message_class": "CommsMessage",
                    "msg_content": {"task_id": task.task_id},
                }
            ),
        )
    )

    assert "Name: Nova" not in assembly.prompt


def test_default_context_engine_hydrates_delivery_context_from_instance_config_each_turn(
    tmp_path,
):
    instance_root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="context_test",
        instance_root=str(instance_root),
        team_id="team_context",
        timezone="America/Chicago",
    )
    config.delivery.quiet_hours.enabled = False
    config.delivery.default_target.channel_name = "telegram"
    config.delivery.default_target.chat_id = "-100123"
    config_path = config.save()

    task = Task(
        task_id="task_context_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="hello",
    )
    message_json = json.dumps(
        {"message_class": "CommsMessage", "msg_content": {"task_id": task.task_id}}
    )
    engine = _engine(
        workspace_dir=str(config.workspace_path),
        instance_root=str(instance_root),
        instance_config_path=str(config_path),
    )

    first = engine.assemble(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(from_agent_id="user", agent_signature=""),
            incoming_meta=MessageMeta(
                from_agent_name="Taylor",
                conversation_id="conversation_context_1",
                msg_correlation_id="corr_context_1",
            ),
            incoming_message_json=message_json,
        )
    )
    assert "Instance Delivery Context:" in first.prompt
    assert "Quiet hours enabled: false" in first.prompt
    assert "Default delivery chat id: -100123" in first.prompt
    assert "Notification routing mode: source_thread_or_default" in first.prompt
    assert "Preferred user communication tool: notify_user" in first.prompt

    updated = GatewayInstanceConfig.load(config_path)
    updated.delivery.quiet_hours.enabled = True
    updated.delivery.quiet_hours.start_hour_local = 21
    updated.delivery.quiet_hours.end_hour_local = 7
    updated.delivery.default_target.chat_id = "-100456"
    updated.save()

    second = engine.assemble(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(from_agent_id="user", agent_signature=""),
            incoming_meta=MessageMeta(
                from_agent_name="Taylor",
                conversation_id="conversation_context_1",
                msg_correlation_id="corr_context_1",
            ),
            incoming_message_json=message_json,
        )
    )
    assert "Quiet hours enabled: true" in second.prompt
    assert "Quiet hours: 21:00-7:00 local time" in second.prompt
    assert "Default delivery chat id: -100456" in second.prompt
    assert "Default delivery chat id: -100123" not in second.prompt
    sources = {item["name"]: item for item in second.source_lengths}
    assert (
        sources["instance_delivery_context"]["source"]
        == "instance_delivery_context.build_instance_delivery_context"
    )


def test_default_context_engine_runs_plugin_lifecycle_hooks():
    class _Plugin:
        def __init__(self):
            self.events = []

        def ingest(self, request):
            self.events.append(("ingest", request.incoming_message_json))

        def assemble(self, request, assembly):
            self.events.append(("assemble", assembly.estimated_tokens))
            assembly.warnings.append({"warning": "plugin_seen"})
            return assembly

        def after_turn(self, request):
            self.events.append(("after_turn", request.task_status))

        def post_invocation(self, request, assembly, **kwargs):
            self.events.append(("post_invocation", kwargs.get("result")))

    plugin = _Plugin()
    task = Task(
        task_id="task_context_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Use the prior turn.",
    )
    engine = _engine(plugins=[plugin])
    assembly = engine.assemble(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(from_agent_id="user", agent_signature=""),
            incoming_meta=MessageMeta(
                from_agent_name="user",
                conversation_id="conversation_context_1",
                msg_correlation_id="corr_context_1",
            ),
            incoming_message_json="{}",
        )
    )
    engine.after_turn(
        TurnFinalizationRequest(
            task_info=object(), task_status="completed", strands_messages=[]
        )
    )
    engine.post_invocation(
        _request(
            incoming_content=task,
            incoming_header=MessageHeader(from_agent_id="user", agent_signature=""),
            incoming_meta=MessageMeta(
                from_agent_name="user",
                conversation_id="conversation_context_1",
                msg_correlation_id="corr_context_1",
            ),
            incoming_message_json="{}",
        ),
        assembly,
        result="ok",
    )

    assert ("ingest", "{}") in plugin.events
    assert any(event[0] == "assemble" for event in plugin.events)
    assert ("after_turn", "completed") in plugin.events
    assert ("post_invocation", "ok") in plugin.events
    assert {"warning": "plugin_seen"} in assembly.warnings
