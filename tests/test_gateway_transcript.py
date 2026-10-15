from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from assistant_gateway.application.gateway_application import (
    GatewayApplicationResponse,
    GatewayDeliveryEvent,
)
from assistant_gateway.application.request_context import GatewayRequestRoute
from assistant_gateway.application.transcript_service import (
    GatewayTranscriptService,
    TranscriptCursorError,
    TranscriptRequestRecord,
)
from assistant_gateway.application.website_services import (
    WebsiteTranscriptApplication,
)
from assistant_gateway.application.website_application import (
    WebTranscriptAttachmentV1,
    WebTranscriptEventPageV1,
    WebTranscriptHistoryPageV1,
    WebTranscriptMessageV1,
)
from assistant_gateway.domain.transcript import TranscriptAppendRequest
from assistant_gateway.runtime_ports import (
    OwnershipPrivateResourceAuthorizer,
    UuidGatewayIdGenerator,
)
from assistant_gateway.store import GatewayStateStore
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.delivery_v1 import UserNotificationAttachment


OWNER = "user_alpha"
OTHER_OWNER = "user_beta"
SESSION = "website:web-chat:alpha"
CONVERSATION = "conversation_alpha"


def _create_web_session(
    store: GatewayStateStore,
    *,
    session_key: str = SESSION,
    conversation_id: str = CONVERSATION,
    owner: str = OWNER,
) -> None:
    store.session_repository.create_session(
        session_key=session_key,
        conversation_id=conversation_id,
        manager_agent_id="manager_1",
        channel_name="website",
        user_id=owner,
        chat_id="web-chat",
        owner_canonical_user_id=owner,
    )


def _service(store: GatewayStateStore) -> GatewayTranscriptService:
    return GatewayTranscriptService(
        repository=store.transcript_repository,
        unit_of_work_factory=store,
        session_repository=store.session_repository,
        id_generator=UuidGatewayIdGenerator(),
    )


def _route(client_request_id: str = "client_request_1") -> GatewayRequestRoute:
    return GatewayRequestRoute(
        channel_name="website",
        user_id=OWNER,
        chat_id="web-chat",
        original_message_id=client_request_id,
        canonical_user_id=OWNER,
    )


def _append_request(
    *,
    message_id: str,
    idempotency_key: str,
    owner: str = OWNER,
    session_key: str = SESSION,
    content: str = "Hello",
) -> TranscriptAppendRequest:
    return TranscriptAppendRequest(
        message_id=message_id,
        session_key=session_key,
        conversation_id=CONVERSATION,
        owner_canonical_user_id=owner,
        channel_name="website",
        chat_id="web-chat",
        role="user",
        content=content,
        message_type="user_message",
        message_level="normal",
        correlation_id=f"corr_{message_id}",
        source_kind="client_request",
        source_id=f"source_{message_id}",
        idempotency_key=idempotency_key,
    )


def test_transcript_history_uses_opaque_gap_free_pages_and_rejects_replaced_stream(
    tmp_path: Path,
) -> None:
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    _create_web_session(store)
    service = _service(store)
    for index in range(1, 6):
        store.transcript_repository.append_transcript_message(
            _append_request(
                message_id=f"message_{index}",
                idempotency_key=f"request_{index}",
                content=f"Message {index}",
            )
        )

    newest = service.history_page(
        owner_canonical_user_id=OWNER,
        session_key=SESSION,
        before_cursor=None,
        limit=2,
    )
    middle = service.history_page(
        owner_canonical_user_id=OWNER,
        session_key=SESSION,
        before_cursor=newest.before_cursor,
        limit=2,
    )
    oldest = service.history_page(
        owner_canonical_user_id=OWNER,
        session_key=SESSION,
        before_cursor=middle.before_cursor,
        limit=2,
    )

    assert [row.content for row in newest.messages] == ["Message 4", "Message 5"]
    assert [row.content for row in middle.messages] == ["Message 2", "Message 3"]
    assert [row.content for row in oldest.messages] == ["Message 1"]
    assert oldest.has_more is False
    assert newest.before_cursor and not newest.before_cursor.isdigit()

    old_cursor = service.snapshot_cursor()
    replacement_store = GatewayStateStore(str(tmp_path / "replacement.db"))
    with pytest.raises(
        TranscriptCursorError,
        match="replaced transcript stream",
    ):
        _service(replacement_store).event_page(
            owner_canonical_user_id=OWNER,
            after_cursor=old_cursor,
        )


def test_user_acceptance_rolls_back_request_and_transcript_when_attachment_is_invalid(
    tmp_path: Path,
) -> None:
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    _create_web_session(store)
    service = _service(store)
    session = store.session_repository.get_session(SESSION)
    assert session is not None
    invalid_attachment = UserTaskAttachment(
        attachment_id="attachment_invalid",
        filename="missing.png",
        content_type="image/png",
        kind="image",
        size_bytes=10,
        sha256="0" * 64,
        storage_refs=[],
    )

    with pytest.raises(ValueError, match="validated local storage reference"):
        service.accept_user_message(
            session=session,
            route=_route(),
            conversation_id=CONVERSATION,
            text="Review this image",
            attachments=(invalid_attachment,),
            request_record=TranscriptRequestRecord(
                correlation_id="corr_atomic",
                request_kind="interactive",
                request_text="Review this image",
                followup_to_task_id=None,
            ),
        )

    assert store.request_repository.get_request("corr_atomic") is None
    assert (
        store.transcript_repository.list_transcript_messages(
            owner_canonical_user_id=OWNER,
            session_key=SESSION,
            before_session_sequence=None,
            limit=10,
        )
        == []
    )
    assert store.delivery_repository.get_attachment("attachment_invalid") is None


def test_response_projection_persists_safe_text_and_authorized_attachment_reference(
    tmp_path: Path,
) -> None:
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    _create_web_session(store)
    service = _service(store)
    session = store.session_repository.get_session(SESSION)
    assert session is not None
    accepted = service.accept_user_message(
        session=session,
        route=_route(),
        conversation_id=CONVERSATION,
        text="Approve this",
        attachments=(),
    )
    assert accepted is not None
    file_path = tmp_path / "reply.txt"
    file_path.write_text("durable attachment", encoding="utf-8")

    response = GatewayApplicationResponse(
        text="Open https://temporary.invalid/capability-secret",
        conversation_id=CONVERSATION,
        correlation_id="corr_reply",
        message_type="secure_credential_collection",
        transcript_text="Credential collection could not be created.",
        attachments=(
            UserNotificationAttachment(
                path=str(file_path),
                filename="reply.txt",
                content_type="text/plain",
                kind="document",
                # Set by the gateway when it staged the copy.
                sha256=hashlib.sha256(b"durable attachment").hexdigest(),
            ),
        ),
    )
    projected = service.append_gateway_response(
        session=session,
        route=_route(),
        response=response,
        reply_to=accepted,
    )

    assert projected is not None
    assert projected.content == "Credential collection could not be created."
    assert "temporary.invalid" not in projected.content
    assert projected.role == "system"
    assert len(projected.attachment_references) == 1
    reference = projected.attachment_references[0]
    assert reference.filename == "reply.txt"
    assert not hasattr(reference, "local_path")
    stored_attachment = store.delivery_repository.get_attachment(
        reference.attachment_id
    )
    assert stored_attachment is not None
    assert stored_attachment.owner_canonical_user_id == OWNER
    # The record keeps the staged copy's digest; the download route checks it.
    assert stored_attachment.sha256 == hashlib.sha256(b"durable attachment").hexdigest()
    assert stored_attachment.local_path == str(file_path)


def test_website_transcript_facet_returns_not_found_across_owners(
    tmp_path: Path,
) -> None:
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    _create_web_session(store)
    service = _service(store)
    application = WebsiteTranscriptApplication(
        credential_actions=lambda owner: (),
        transcript=service,
        session_repository=store.session_repository,
        private_authorizer=OwnershipPrivateResourceAuthorizer(),
        team_id="team_1",
    )
    exchange = application.record_local_exchange(
        owner_canonical_user_id=OWNER,
        session_key=SESSION,
        conversation_id=CONVERSATION,
        client_request_id="client_command_1",
        user_text="/help",
        response_text="Safe help text",
        response_message_type="command",
    )

    assert exchange is not None
    assert (
        application.list_messages(
            owner_canonical_user_id=OTHER_OWNER,
            session_key=SESSION,
            before_cursor=None,
            limit=50,
        )
        is None
    )
    assert (
        application.list_events(
            owner_canonical_user_id=OTHER_OWNER,
            after_cursor=None,
            limit=50,
        ).messages
        == ()
    )
    owner_page = application.list_messages(
        owner_canonical_user_id=OWNER,
        session_key=SESSION,
        before_cursor=None,
        limit=50,
    )
    assert owner_page is not None
    assert [message.role for message in owner_page.messages] == ["user", "system"]


def test_an_access_review_announcement_carries_its_request_id_once(
    tmp_path: Path,
) -> None:
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    _create_web_session(store)
    service = _service(store)
    application = WebsiteTranscriptApplication(
        credential_actions=lambda owner: (),
        transcript=service,
        session_repository=store.session_repository,
        private_authorizer=OwnershipPrivateResourceAuthorizer(),
        team_id="team_1",
    )

    def announcement() -> GatewayDeliveryEvent:
        return GatewayDeliveryEvent(
            text="TeamArchitect asks for approval: Switch TeamArchitect to b. Takes effect now.",
            session_key=SESSION,
            channel_name="website",
            user_id=OWNER,
            chat_id="web-chat",
            thread_id=None,
            conversation_id=CONVERSATION,
            scheduled_job_id=None,
            correlation_id=None,
            original_message_id=None,
            message_type="access_review",
            access_request_id="access_req_1",
            review_choices=("approve_all", "deny"),
        )

    first = service.append_delivery_event(announcement())
    again = service.append_delivery_event(announcement())
    ordinary = service.append_delivery_event(
        GatewayDeliveryEvent(
            text="Done.",
            session_key=SESSION,
            channel_name="website",
            user_id=OWNER,
            chat_id="web-chat",
            thread_id=None,
            conversation_id=CONVERSATION,
            scheduled_job_id=None,
            correlation_id="corr_1",
            original_message_id=None,
        )
    )
    page = application.list_messages(
        owner_canonical_user_id=OWNER, session_key=SESSION, before_cursor=None, limit=50
    )

    assert first is not None and again is not None and ordinary is not None
    assert again.message_id == first.message_id, "one message per request in a conversation"
    assert page is not None
    [review, reply] = page.messages
    assert review.role == "system" and review.message_type == "access_review"
    assert review.access_request_id == "access_req_1"
    assert reply.access_request_id is None


def test_gateway_transcript_contract_schemas_and_conformance_fixtures() -> None:
    contract_dir = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "schemas"
        / "gateway"
        / "transcript"
        / "v1"
    )
    manifest = json.loads((contract_dir / "manifest.json").read_text(encoding="utf-8"))
    fixture_path = contract_dir / manifest["fixtures"]["filename"]
    assert (
        hashlib.sha256(fixture_path.read_bytes()).hexdigest()
        == (manifest["fixtures"]["sha256"])
    )
    fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))
    models = {
        model.__name__: model
        for model in (
            WebTranscriptAttachmentV1,
            WebTranscriptMessageV1,
            WebTranscriptHistoryPageV1,
            WebTranscriptEventPageV1,
        )
    }
    validators: dict[str, Draft202012Validator] = {}
    for entry in manifest["schemas"]:
        schema_path = contract_dir / entry["filename"]
        schema_text = schema_path.read_text(encoding="utf-8")
        assert (
            hashlib.sha256(schema_text.encode("utf-8")).hexdigest() == entry["sha256"]
        )
        schema = json.loads(schema_text)
        Draft202012Validator.check_schema(schema)
        validators[entry["model"]] = Draft202012Validator(schema)

    for case in fixtures["valid"]:
        models[case["model"]].model_validate(case["payload"])
        validators[case["model"]].validate(case["payload"])
    for case in fixtures["invalid"]:
        with pytest.raises(ValidationError):
            models[case["model"]].model_validate(case["payload"])
        assert list(validators[case["model"]].iter_errors(case["payload"]))
