from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_agents.automation_runtime import handle_automation_message
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.schedule_v1 import (
    AutomationDeliveryPreferenceSnapshot,
    AutomationEnvelopeSnapshot,
    AutomationMessage,
    AutomationQuietHoursWindow,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType


class _FakeTaskManager:
    def __init__(self) -> None:
        from custom_agents.task_operation_runtime import TaskOperationRuntime

        self.operations_runtime = TaskOperationRuntime()
        self.task_info_by_id: dict[str, SimpleNamespace] = {}
        self.history_entries: list[dict[str, object]] = []

    def get_task_info(self, task_id: str):
        return self.task_info_by_id.get(task_id)

    def append_history_entry(
        self, task_id: str, summary: str, *, entry_type, importance, details
    ):
        self.history_entries.append(
            {
                "task_id": task_id,
                "summary": summary,
                "entry_type": entry_type,
                "importance": importance,
                "details": details,
            }
        )


class _FakeAgent:
    def __init__(self) -> None:
        self.task_manager = _FakeTaskManager()
        self.captured_task = None
        self.captured_meta = None
        self.captured_header = None

    async def _handle_task_request(
        self, task_request: Task, msg_header: MessageHeader, msg_meta: MessageMeta
    ) -> None:
        self.captured_task = task_request
        self.captured_header = msg_header
        self.captured_meta = msg_meta
        self.task_manager.task_info_by_id[task_request.task_id] = SimpleNamespace(
            task=task_request,
            header=msg_header,
            meta=msg_meta,
        )


@pytest.mark.asyncio
async def test_handle_automation_message_synthesizes_task_and_history():
    agent = _FakeAgent()
    original_envelope = MessageEnvelope(
        msg_content=Task(
            task_id="task_original_123",
            task_type=MessageMetaType.REQUEST,
            problem_statement="Original user request",
        ),
        msg_meta=MessageMeta(
            conversation_id="conversation_original_123",
            request_timezone="America/Chicago",
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=GatewayContext(
                session_key="session_original_123",
                canonical_user_id="test-user",
            ),
        ),
        msg_header=MessageHeader(
            from_agent_id="gateway_queue",
            to_routing_path="manager_queue",
            response_routing_key="gateway_queue",
        ),
    )
    automation_message = AutomationMessage(
        automation_id="automation_test_123",
        hook_name="after_user_reply",
        original_envelope_snapshot=AutomationEnvelopeSnapshot(
            msg_header=original_envelope.msg_header.model_copy(deep=True),
            msg_meta=original_envelope.msg_meta.model_copy(deep=True),
            msg_content=original_envelope.msg_content.model_dump(mode="json"),
        ),
        rendered_original_user_text="Original user request",
        delivery_preference_snapshot=AutomationDeliveryPreferenceSnapshot(
            preferred_channel="telegram",
            preferred_output_chat_id="chat_123",
            preferred_channel_thread_id="thread_123",
            summarize_deferred=True,
            digest_delivery_hour_local=8,
            urgent_keywords=["urgent", "critical"],
        ),
        requester_local_time="2026-04-17T10:30:00-05:00",
        quiet_hours_window=AutomationQuietHoursWindow(
            timezone_name="America/Chicago",
            enabled=True,
            start_hour_local=22,
            end_hour_local=8,
            allow_urgent_delivery=True,
            currently_within_quiet_hours=False,
        ),
        suggested_later_time="2026-04-18T08:00:00+00:00",
        allowed_decisions=["reply_now", "defer"],
        decision_instructions="Only reply if the content is actionable.",
    )

    await handle_automation_message(
        agent,
        automation_message=automation_message,
        msg_header=original_envelope.msg_header,
        msg_meta=MessageMeta(
            msg_correlation_id="corr_automation_123",
            gateway_context=GatewayContext(session_key="session_override_123"),
        ),
    )

    assert agent.captured_task is not None
    assert agent.captured_task.task_type == MessageMetaType.REQUEST
    assert "[Automation Context]" in agent.captured_task.problem_statement
    assert "Automation ID: automation_test_123" in agent.captured_task.problem_statement
    assert "Original user request" in agent.captured_task.problem_statement
    assert '"task_id": "task_original_123"' in agent.captured_task.problem_statement

    assert agent.captured_meta is not None
    assert agent.captured_meta.requester_type == RequesterType.TIMER_REQUESTER
    assert agent.captured_meta.is_response_requested is True
    assert agent.captured_meta.conversation_id == "conversation_original_123"
    assert agent.captured_meta.request_timezone == "America/Chicago"
    assert agent.captured_meta.followup_to_task_id == "task_original_123"
    assert agent.captured_meta.gateway_context is not None
    assert agent.captured_meta.gateway_context.session_key == "session_override_123"

    assert len(agent.task_manager.history_entries) == 1
    history = agent.task_manager.history_entries[0]
    assert history["details"]["automation_id"] == "automation_test_123"
    assert history["details"]["hook_name"] == "after_user_reply"
    assert history["details"]["source_task_id"] == "task_original_123"
