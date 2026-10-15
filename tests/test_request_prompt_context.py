from __future__ import annotations


from custom_agents.request_prompt_context import (
    build_request_conversation_context,
)
from custom_messages.contracts.routing_v1 import MessageMeta
from custom_messages.enums import RequesterType


def test_cross_conversation_recall_hint_is_user_request_only() -> None:
    request_text = "What did we talk about in previous conversations?"

    user_context = build_request_conversation_context(
        MessageMeta(conversation_id="conversation_user", requester_type=RequesterType.USER_REQUESTER),
        request_text=request_text,
    )
    agent_context = build_request_conversation_context(
        MessageMeta(
            conversation_id="conversation_internal",
            requester_type=RequesterType.AGENT_REQUESTER,
            msg_correlation_id="memory_review_1234",
        ),
        request_text=request_text,
    )
    timer_context = build_request_conversation_context(
        MessageMeta(
            conversation_id="conversation_schedule",
            requester_type=RequesterType.TIMER_REQUESTER,
            msg_correlation_id="schedule_corr_sched_1",
        ),
        request_text=request_text,
    )

    assert "list_conversations" in user_context
    assert "list_conversations" not in agent_context
    assert "list_conversations" not in timer_context
