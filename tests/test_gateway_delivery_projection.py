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


def test_delivery_ref_scan_logs_when_limit_reached_and_queries_only_unowned_sessions(
    monkeypatch, caplog
) -> None:
    """C31: hitting the delivery_ref scan limit logs a warning and checks only ownerless sessions, not all people."""

    import logging
    from types import SimpleNamespace

    import pytest

    from assistant_gateway.application import delivery_projection as mod
    from assistant_gateway.application.delivery_projection import (
        DeliveryPersonMismatch,
        GatewayDeliveryProjection,
        GatewayDeliveryProjectionSettings,
    )
    from custom_messages.contracts.envelope_v1 import MessageEnvelope
    from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.enums import MessageMetaType

    monkeypatch.setattr(mod, "_DELIVERY_REF_SCAN_LIMIT", 2)
    owners_session = SimpleNamespace(session_key="session_first", owner_canonical_user_id="usr_first")
    list_calls: list[tuple[int, str | None]] = []
    unowned_calls: list[int] = []

    def _list_sessions(*, limit: int = 50, status=None, owner_canonical_user_id=None):
        list_calls.append((limit, owner_canonical_user_id))
        return [
            SimpleNamespace(session_key="session_ben_1", owner_canonical_user_id="usr_ben"),
            SimpleNamespace(session_key="session_ben_2", owner_canonical_user_id="usr_ben"),
        ]

    def _list_unowned(*, limit: int = 50):
        unowned_calls.append(limit)
        return [
            SimpleNamespace(session_key="legacy_1", owner_canonical_user_id=None),
            SimpleNamespace(session_key="legacy_2", owner_canonical_user_id=None),
        ]

    projection = GatewayDeliveryProjection(
        settings=GatewayDeliveryProjectionSettings(default_session_key="session_first"),
        sessions=SimpleNamespace(
            get_session=lambda key: owners_session if key == "session_first" else None,
            get_session_by_conversation_id=lambda _conversation: None,
            list_sessions=_list_sessions,
            list_unowned_sessions=_list_unowned,
        ),
        delivery=SimpleNamespace(get_scheduled_target=lambda _id: None),
        presentation=SimpleNamespace(),
        attachments=SimpleNamespace(),
        delivery_refs=SimpleNamespace(ref=lambda key: f"dr_{key}"),
        logger=logging.getLogger("test.projection.c31"),
    )
    envelope = MessageEnvelope(
        msg_content=Task(task_type=MessageMetaType.RESPONSE, problem_statement="Ben's reminder."),
        msg_header=MessageHeader(from_agent_id="agent"),
        msg_meta=MessageMeta(
            gateway_context=GatewayContext(
                canonical_user_id="usr_ben",
                delivery_ref="dr_unmatched_session",
                scheduled_job_id="sched_unmatched",
            )
        ),
    )

    with caplog.at_level(logging.WARNING, logger="test.projection.c31"):
        with pytest.raises(DeliveryPersonMismatch):
            projection.project(envelope=envelope, rendered_text="Ben's reminder.", request_record=None)

    assert list_calls == [(2, "usr_ben")]
    assert unowned_calls == [2]
    assert "event=gateway_delivery_ref_scan_limit_reached scope=person" in caplog.text
    assert "event=gateway_delivery_ref_scan_limit_reached scope=unowned" in caplog.text


def test_delivery_ref_does_not_overwrite_another_persons_scheduled_target(caplog) -> None:
    """C35: mismatched reply metadata must never overwrite another person's scheduled target."""

    import logging
    from types import SimpleNamespace

    from assistant_gateway.application.delivery_projection import (
        GatewayDeliveryProjection,
        GatewayDeliveryProjectionSettings,
    )
    from custom_messages.contracts.envelope_v1 import MessageEnvelope
    from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.enums import MessageMetaType

    ada_target = SimpleNamespace(
        scheduled_job_id="sched_ada",
        session_key="website:ada:chat",
        conversation_id="conv_ada",
        owner_canonical_user_id="usr_ada",
    )
    bo_session = SimpleNamespace(
        session_key="website:bo:chat",
        channel_name="website",
        user_id="bo",
        chat_id="chat_bo",
        thread_id=None,
        conversation_id="conv_bo",
        owner_canonical_user_id="usr_bo",
    )
    upserts: list[dict[str, object]] = []

    projection = GatewayDeliveryProjection(
        settings=GatewayDeliveryProjectionSettings(default_session_key="website:ada:chat"),
        sessions=SimpleNamespace(
            get_session=lambda key: bo_session if key == "website:bo:chat" else None,
            get_session_by_conversation_id=lambda _conversation: None,
            list_sessions=lambda *, limit=50, status=None, owner_canonical_user_id=None: (
                [bo_session] if owner_canonical_user_id == "usr_bo" else []
            ),
            list_unowned_sessions=lambda *, limit=50: [],
        ),
        delivery=SimpleNamespace(
            get_scheduled_target=lambda job_id: ada_target if job_id == "sched_ada" else None,
            upsert_scheduled_target=lambda **kwargs: upserts.append(kwargs),
        ),
        presentation=SimpleNamespace(message_type=lambda _c: "response", message_level=lambda _c: "info"),
        attachments=SimpleNamespace(resolve=lambda _content, *, person_id=None: ()),
        delivery_refs=SimpleNamespace(ref=lambda key: f"dr_{key}"),
        logger=logging.getLogger("test.projection.c35"),
    )
    envelope = MessageEnvelope(
        msg_content=Task(task_type=MessageMetaType.RESPONSE, problem_statement="Mismatched reply."),
        msg_header=MessageHeader(from_agent_id="agent"),
        msg_meta=MessageMeta(
            msg_correlation_id="corr_mismatch_1",
            gateway_context=GatewayContext(
                canonical_user_id="usr_bo",
                delivery_ref="dr_website:bo:chat",
                scheduled_job_id="sched_ada",
            ),
        ),
    )

    with caplog.at_level(logging.WARNING, logger="test.projection.c35"):
        event = projection.project(envelope=envelope, rendered_text="Mismatched reply.", request_record=None)

    assert event.session_key == "website:bo:chat"
    assert upserts == [], "existing target owned by usr_ada must not be overwritten"
    assert "event=gateway_scheduled_target_owner_mismatch" in caplog.text
    assert "sched_ada" in caplog.text
    assert "usr_ada" in caplog.text
    assert "usr_bo" in caplog.text
    assert "corr_mismatch_1" in caplog.text


def test_failed_scheduled_target_write_is_logged_and_delivery_continues(caplog) -> None:
    """C35: a storage error while caching a scheduled target must not prevent reply delivery."""

    import logging
    from types import SimpleNamespace

    from assistant_gateway.application.delivery_projection import (
        GatewayDeliveryProjection,
        GatewayDeliveryProjectionSettings,
    )
    from custom_messages.contracts.envelope_v1 import MessageEnvelope
    from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.enums import MessageMetaType

    ada_session = SimpleNamespace(
        session_key="website:ada:chat",
        channel_name="website",
        user_id="ada",
        chat_id="chat_ada",
        thread_id=None,
        conversation_id="conv_ada",
        owner_canonical_user_id="usr_ada",
    )

    def _boom(**_kwargs):
        raise RuntimeError("database locked")

    projection = GatewayDeliveryProjection(
        settings=GatewayDeliveryProjectionSettings(default_session_key="website:ada:chat"),
        sessions=SimpleNamespace(
            get_session=lambda key: ada_session if key == "website:ada:chat" else None,
            get_session_by_conversation_id=lambda _conversation: None,
            list_sessions=lambda *, limit=50, status=None, owner_canonical_user_id=None: [ada_session],
            list_unowned_sessions=lambda *, limit=50: [],
        ),
        delivery=SimpleNamespace(
            get_scheduled_target=lambda _job_id: None,
            upsert_scheduled_target=_boom,
        ),
        presentation=SimpleNamespace(message_type=lambda _c: "response", message_level=lambda _c: "info"),
        attachments=SimpleNamespace(resolve=lambda _content, *, person_id=None: ()),
        delivery_refs=SimpleNamespace(ref=lambda key: f"dr_{key}"),
        logger=logging.getLogger("test.projection.c35_write_err"),
    )
    envelope = MessageEnvelope(
        msg_content=Task(task_type=MessageMetaType.RESPONSE, problem_statement="Reminder."),
        msg_header=MessageHeader(from_agent_id="agent"),
        msg_meta=MessageMeta(
            msg_correlation_id="corr_write_err_1",
            gateway_context=GatewayContext(
                canonical_user_id="usr_ada",
                delivery_ref="dr_website:ada:chat",
                scheduled_job_id="sched_ada_new",
            ),
        ),
    )

    with caplog.at_level(logging.WARNING, logger="test.projection.c35_write_err"):
        event = projection.project(envelope=envelope, rendered_text="Reminder.", request_record=None)

    assert event.session_key == "website:ada:chat"
    assert "event=gateway_scheduled_target_record_failed" in caplog.text
    assert "database locked" in caplog.text
