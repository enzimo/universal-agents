from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.interactive_apps.delivery_projection import (
    preview_text,
    project_flattened_delivery,
    project_text_delivery,
)
from custom_messages.contracts.delivery_v1 import UserNotificationAttachment


def _event(*, attachments=()) -> GatewayDeliveryEvent:
    return GatewayDeliveryEvent(
        text="Hello",
        session_key="website:user:chat",
        channel_name="website",
        user_id="user",
        chat_id="chat",
        thread_id="thread",
        conversation_id="conversation",
        scheduled_job_id=None,
        correlation_id="correlation",
        original_message_id="original",
        message_type="response",
        attachments=attachments,
    )


def test_preview_text_normalizes_and_handles_small_limits() -> None:
    assert preview_text("  hello\n world  ") == "hello world"
    assert preview_text("abcdef", limit=5) == "ab..."
    assert preview_text("abcdef", limit=2) == "..."


def test_text_projection_preserves_thread_and_separate_attachment_text() -> None:
    event = _event(attachments=(UserNotificationAttachment(path="/tmp/report.txt"),))

    outbound = project_text_delivery(event, channel_name="telegram")

    assert outbound.channel_name == "telegram"
    assert outbound.thread_id == "thread"
    assert outbound.text == "Hello"
    assert outbound.in_reply_to_message_id == "original"


def test_flattened_projection_includes_attachment_paths_without_thread() -> None:
    event = _event(
        attachments=(
            UserNotificationAttachment(
                path="/tmp/report.txt", filename="Operator report"
            ),
        )
    )

    outbound = project_flattened_delivery(event, channel_name="signal")

    assert outbound.channel_name == "signal"
    assert outbound.thread_id is None
    assert outbound.text == "Hello\nAttachments:\n- Operator report: /tmp/report.txt"


def test_a_message_for_one_person_is_never_delivered_to_another_persons_default_session() -> None:
    """With no route to its person, a message is refused instead of falling back to the owner's session."""

    import logging
    from types import SimpleNamespace

    import pytest

    from assistant_gateway.application.delivery_projection import (
        DeliveryPersonMismatch,
        GatewayDeliveryProjection,
        GatewayDeliveryProjectionSettings,
    )
    from custom_messages.contracts.envelope_v1 import MessageEnvelope
    from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.enums import MessageMetaType

    owners_session = SimpleNamespace(session_key="session_first", owner_canonical_user_id="usr_first")
    projection = GatewayDeliveryProjection(
        settings=GatewayDeliveryProjectionSettings(default_session_key="session_first"),
        sessions=SimpleNamespace(
            get_session=lambda key: owners_session if key == "session_first" else None,
            get_session_by_conversation_id=lambda _conversation: None,
        ),
        delivery=SimpleNamespace(get_scheduled_target=lambda _id: None),
        presentation=SimpleNamespace(),
        attachments=SimpleNamespace(),
        logger=logging.getLogger("test.projection"),
    )
    envelope = MessageEnvelope(
        msg_content=Task(task_type=MessageMetaType.RESPONSE, problem_statement="Ben's notice."),
        msg_header=MessageHeader(from_agent_id="agent"),
        msg_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id="usr_ben")),
    )

    with pytest.raises(DeliveryPersonMismatch):
        projection.project(envelope=envelope, rendered_text="Ben's notice.", request_record=None)
